from ..kdb import *
from ..kdb import _add_uniform
from ..utils import *
from pgmpy.models import BayesianNetwork
from pgmpy.sampling import BayesianModelSampling
from pgmpy.factors.discrete import TabularCPD
import numpy as np
import tensorflow as tf

class GANBLR:
    """
    The GANBLR Model.
    """
    def __init__(self) -> None:
        self.__d = None
        self.__gen_weights = None
        self.batch_size = None
        self.epochs = None
        self.k = None
        self.constraints = None
    
    def fit(self, x, y, k=0, batch_size=32, epochs=10, warmup_epochs=1, verbose=1):
        '''
        Fit the model to the given data.

        Parameters:
        --------
        x, y (numpy.ndarray): Dataset to fit the model. The data should be discrete.
        
        k (int, optional): Parameter k of ganblr model. Must be greater than 0. No more than 2 is Suggested.

        batch_size (int, optional): Size of the batch to feed the model at each step. Defaults to
            :attr:`32`.
        
        epochs (int, optional): Number of epochs to use during training. Defaults to :attr:`10`.
        
        warmup_epochs (int, optional): Number of epochs to use in warmup phase. Defaults to :attr:`1`.
        
        TODO: verbose (int, optional): Whether to output the log. Use 1 for log output and 0 for complete silence.
        '''
        if verbose is None or not isinstance(verbose, int):
            verbose = 1
        d = DataUtils(x, y)
        self.__d = d
        self.k = k
        self.batch_size = batch_size
        history = self._warmup_run(warmup_epochs)
        syn_x, syn_y = self.sample() 
        discriminator_label = np.hstack([np.ones(d.data_size), np.zeros(d.data_size)])
        for i in range(epochs):
            discriminator_input = np.vstack([x, syn_x])
            disc_input, disc_label = sample(discriminator_input, discriminator_label, frac=0.8)
            disc = self._discrim()
            disc.fit(disc_input, disc_label, batch_size=batch_size, epochs=1)
            prob_fake = disc.predict(x)
            ls = np.mean(-np.log(np.subtract(1, prob_fake)))
            history = self._run_generator(loss=ls)
            syn_x, syn_y = self.sample() 
        
        return self
        
    def evaluate(self, x, y, model='lr', preprocesser=''):
        """
        Perform a TSTR(Training on Synthetic data, Testing on Real data) evaluation.

        Parameters:
        ------------------
         x, y (numpy.ndarray): test dataset.

         model: the model used for evaluate. Should be one of ['lr', 'mlp', 'rf'], or a model class that have sklearn-style 'fit' method.

        Return:
        --------
        accuracy score (float).

        """
        from sklearn.linear_model import LogisticRegression
        from sklearn.neural_network import MLPClassifier
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        from sklearn.metrics import accuracy_score
        
        eval_model = None
        if model=='lr':
            eval_model = Pipeline([
                ('scaler', StandardScaler()), 
                ('lr',     LogisticRegression())]) 
        elif model == 'rf':
            eval_model = RandomForestClassifier()
        elif model == 'mlp':
            eval_model = Pipeline([
                ('scaler', StandardScaler()), 
                ('mlp',    MLPClassifier())]) 
        elif hasattr(model, 'fit'):
            eval_model = model
        else:
            raise Exception('Invalid Arugument')
        
        synthetic_data = self.sample()
        eval_model.fit(*synthetic_data)
        pred = eval_model.predict(x)
        return accuracy_score(y, pred)
    
    def sample(self, size=None, ohe=False, verbose=1):
        """
        Generate synthetic data.     

        Parameters:
        -----------------
        size (int, optional): Size of the data to be generated. Defaults to: attr:`None`.

        ohe (bool, optional): Whether to convert the data into one-hot form. Defaults to: attr:`False`.

        TODO: verbose (int, optional): Whether to output the log. Use 1 for log output and 0 for complete silence.

        Return:
        -----------------
        x, y (np.ndarray): Generated synthetic data.

        """
        if verbose is None or not isinstance(verbose, int):
            verbose = 1
        #basic varibles
        d = self.__d
        feature_cards = np.array(d.feature_uniques)
        #ensure sum of each constraint group equals to 1, then re concat the probs
        _idxs = np.cumsum([0] + d._kdbe.constraints_.tolist())
        constraint_idxs = [(_idxs[i],_idxs[i+1]) for i in range(len(_idxs)-1)]
        
        probs = np.exp(self.__gen_weights[0])
        cpd_probs = [probs[start:end,:] for start, end in constraint_idxs]
        cpd_probs = np.vstack([p/p.sum(axis=0) for p in cpd_probs])
    
        #assign the probs to the full cpd tables
        idxs = np.cumsum([0] + d._kdbe.high_order_feature_uniques_)
        feature_idxs = [(idxs[i],idxs[i+1]) for i in range(len(idxs)-1)]
        have_value_idxs = d._kdbe.have_value_idxs_
        full_cpd_probs = [] 
        for have_value, (start, end) in zip(have_value_idxs, feature_idxs):
            #(n_high_order_feature_uniques, n_classes)
            cpd_prob_ = cpd_probs[start:end,:]
            #(n_all_combination) Note: the order is (*parent, variable)
            have_value_ravel = have_value.ravel()
            #(n_classes * n_all_combination)
            have_value_ravel_repeat = np.hstack([have_value_ravel] * d.num_classes)
            #(n_classes * n_all_combination) <- (n_classes * n_high_order_feature_uniques)
            full_cpd_prob_ravel = np.zeros_like(have_value_ravel_repeat, dtype=float)
            full_cpd_prob_ravel[have_value_ravel_repeat] = cpd_prob_.T.ravel()
            #(n_classes * n_parent_combinations, n_variable_unique)
            full_cpd_prob = full_cpd_prob_ravel.reshape(-1, have_value.shape[-1]).T
            full_cpd_prob = _add_uniform(full_cpd_prob, noise=0)
            full_cpd_probs.append(full_cpd_prob)
    
        #prepare node and edge names
        node_names = [str(i) for i in range(d.num_features + 1)]
        edge_names = [(str(i), str(j)) for i,j in d._kdbe.edges_]
        y_name = node_names[-1]
    
        #create TabularCPD objects
        evidences = d._kdbe.dependencies_
        feature_cpds = [
            TabularCPD(str(name), feature_cards[name], table, 
                       evidence=[y_name, *[str(e) for e in evidences]], 
                       evidence_card=[d.num_classes, *feature_cards[evidences].tolist()])
            for (name, evidences), table in zip(evidences.items(), full_cpd_probs)
        ]
        y_probs = (d.class_counts/d.data_size).reshape(-1,1)
        y_cpd = TabularCPD(y_name, d.num_classes, y_probs)
    
        #create kDB model, then sample data

        model = BayesianNetwork(edge_names)
        model.add_cpds(y_cpd, *feature_cpds)
        sample_size = d.data_size if size is None else size
        result = BayesianModelSampling(model).forward_sample(size=sample_size, show_progress = verbose > 0)
        sorted_result = result[node_names].values
    
        #return
        syn_X, syn_y = sorted_result[:,:-1], sorted_result[:,-1]
        if ohe:
            from sklearn.preprocessing import OneHotEncoder
            ohe_syn_X = OneHotEncoder().fit_transform(syn_X)
            return ohe_syn_X, syn_y
        else:
            return syn_X, syn_y

    def _warmup_run(self, epochs):
        d = self.__d
        tf.keras.backend.clear_session()
        ohex = d.get_kdbe_x(self.k)
        self.constraints = softmax_weight(d.constraint_positions)
        elr = get_lr(ohex.shape[1], d.num_classes, self.constraints)
        history = elr.fit(ohex, d.y, batch_size=self.batch_size, epochs=epochs)
        self.__gen_weights = elr.get_weights()
        tf.keras.backend.clear_session()
        return history

    def _run_generator(self, loss):
        d = self.__d
        ohex = d.get_kdbe_x(self.k)
        tf.keras.backend.clear_session()
        model = tf.keras.Sequential()
        model.add(tf.keras.layers.Dense(d.num_classes, input_dim=ohex.shape[1], activation='softmax',kernel_constraint=self.constraints))
        model.compile(loss=elr_loss(loss), optimizer='adam', metrics=['accuracy'])
        model.set_weights(self.__gen_weights)
        history = model.fit(ohex, d.y, batch_size=self.batch_size,epochs=1)
        self.__gen_weights = model.get_weights()
        tf.keras.backend.clear_session()
        return history
    
    def _discrim(self):
        model = tf.keras.Sequential()
        model.add(tf.keras.layers.Dense(1, input_dim=self.__d.num_features, activation='sigmoid'))
        model.compile(loss='binary_crossentropy', optimizer='adam', metrics=['accuracy'])
        return model